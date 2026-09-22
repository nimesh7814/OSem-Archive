import type { FeatureCollection, Point } from 'geojson'

type PublicApiFeature = GeoJSON.Feature<Point> & {
	properties: {
		_id?: string
		id?: string
		name?: string
		exposure?: string
		status?: string
		lastMeasurementAt?: string
		tags?: string[]
		sensors?: Array<{ title?: string }>
		[key: string]: unknown
	}
}

type PublicApiTags = {
	code?: string
	data?: string[]
}

type PublicApiError = {
	code?: string
	message?: string
	error?: string
}

type PublicApiSensor = {
	_id?: string
	id?: string
	title?: string
	unit?: string
	sensorType?: string
	icon?: string
	status?: string
	lastMeasurement?: {
		value?: number | string
		createdAt?: string
	} | null
	[key: string]: unknown
}

type PublicApiDevice = {
	_id?: string
	id?: string
	name?: string
	description?: string
	image?: string | null
	link?: string | null
	website?: string | null
	tags?: string[]
	exposure?: string
	model?: string
	currentLocation?: {
		timestamp?: string
		coordinates?: number[]
		type?: string
	}
	loc?: Array<{
		geometry?: {
			timestamp?: string
			coordinates?: number[]
			type?: string
		}
	}>
	sensors?: PublicApiSensor[]
	createdAt?: string
	updatedAt?: string
	lastMeasurementAt?: string
	[key: string]: unknown
}

// The public API's full box list takes 15-60s to fetch, so cache it for a
// while rather than re-fetching on every /explore request.
const CACHE_TTL_MS = 5 * 60_000

let devicesCache:
	| {
			expiresAt: number
			data: FeatureCollection<Point, any>
	  }
	| undefined

function getApiBaseUrl() {
	return (process.env.OSEM_API_URL || 'https://api.opensensemap.org/').replace(
		/\/+$/,
		'',
	)
}

async function fetchJson<T>(path: string): Promise<T> {
	const response = await fetch(`${getApiBaseUrl()}${path}`, {
		headers: { Accept: 'application/json' },
	})

	if (!response.ok) {
		throw new Response(`openSenseMap API request failed: ${path}`, {
			status: response.status,
		})
	}

	return response.json() as Promise<T>
}

async function postPublicApi<T>(
	path: string,
	body: Record<string, unknown>,
): Promise<
	| {
			ok: true
			data: T
	  }
	| {
			ok: false
			status: number
			message: string
	  }
> {
	const response = await fetch(`${getApiBaseUrl()}${path}`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
		},
		body: JSON.stringify(body),
	})

	let payload: PublicApiError | T | undefined
	try {
		payload = (await response.json()) as PublicApiError | T
	} catch {
		payload = undefined
	}

	if (!response.ok) {
		const errorPayload = payload as PublicApiError | undefined
		return {
			ok: false,
			status: response.status,
			message:
				errorPayload?.message ??
				errorPayload?.error ??
				`openSenseMap API request failed with status ${response.status}`,
		}
	}

	return {
		ok: true,
		data: payload as T,
	}
}

function getStatus(lastMeasurementAt: string | undefined) {
	if (!lastMeasurementAt) return 'old'

	const ageMs = Date.now() - new Date(lastMeasurementAt).getTime()
	if (!Number.isFinite(ageMs)) return 'old'

	if (ageMs < 7 * 24 * 60 * 60 * 1000) return 'active'
	if (ageMs < 30 * 24 * 60 * 60 * 1000) return 'inactive'
	return 'old'
}

function normalizeFeature(feature: PublicApiFeature) {
	const [longitude, latitude] = feature.geometry.coordinates
	const id = feature.properties.id ?? feature.properties._id ?? ''
	const status = feature.properties.status ?? getStatus(feature.properties.lastMeasurementAt)

	return {
		...feature,
		properties: {
			...feature.properties,
			id,
			_id: feature.properties._id ?? id,
			latitude,
			longitude,
			exposure: feature.properties.exposure ?? 'unknown',
			status,
			tags: feature.properties.tags ?? [],
			sensors: feature.properties.sensors ?? [],
		},
	}
}

function normalizeSensor(sensor: PublicApiSensor, order: number) {
	const id = sensor.id ?? sensor._id ?? ''
	const createdAt = sensor.lastMeasurement?.createdAt
	const status = sensor.status ?? getStatus(createdAt)

	return {
		...sensor,
		id,
		_id: sensor._id ?? id,
		sensorType: sensor.sensorType ?? '',
		status,
		order,
		lastMeasurement: sensor.lastMeasurement
			? {
					...sensor.lastMeasurement,
					createdAt: createdAt ?? new Date().toISOString(),
				}
			: null,
	}
}

function normalizeLocation(location: NonNullable<PublicApiDevice['loc']>[number]) {
	const coordinates = location.geometry?.coordinates ?? []
	const [x, y] = coordinates

	if (typeof x !== 'number' || typeof y !== 'number') return null

	return {
		time: location.geometry?.timestamp ?? new Date().toISOString(),
		geometry: { x, y },
	}
}

function isNormalizedLocation(
	location: ReturnType<typeof normalizeLocation>,
): location is NonNullable<ReturnType<typeof normalizeLocation>> {
	return location !== null
}

function normalizeDevice(device: PublicApiDevice) {
	const id = device.id ?? device._id ?? ''
	const currentCoordinates = device.currentLocation?.coordinates ?? []
	const longitude = currentCoordinates[0] ?? null
	const latitude = currentCoordinates[1] ?? null
	const updatedAt =
		device.updatedAt ?? device.lastMeasurementAt ?? device.createdAt ?? new Date().toISOString()
	const createdAt = device.createdAt ?? updatedAt
	const sensors = (device.sensors ?? []).map(normalizeSensor)

	return {
		...device,
		id,
		_id: device._id ?? id,
		latitude,
		longitude,
		status: getStatus(device.lastMeasurementAt ?? updatedAt),
		createdAt,
		updatedAt,
		expiresAt: null,
		tags: device.tags ?? [],
		logEntries: [],
		locations: (device.loc ?? [])
			.map(normalizeLocation)
			.filter(isNormalizedLocation),
		sensors,
		sensorWikiModel: device.model ?? null,
		userId: null,
	}
}

export async function getPublicDevicesGeoJson() {
	const now = Date.now()
	if (devicesCache && devicesCache.expiresAt > now) return devicesCache.data

	const devices = await fetchJson<FeatureCollection<Point, any>>(
		'/boxes?format=geojson&minimal=true',
	)

	const normalizedDevices = {
		...devices,
		features: devices.features.map((feature) =>
			normalizeFeature(feature as PublicApiFeature),
		),
	}

	devicesCache = {
		data: normalizedDevices,
		expiresAt: now + CACHE_TTL_MS,
	}

	return normalizedDevices
}

export async function getArchiveMeasurementCount() {
	const archiveApiUrl = (
		process.env.ARCHIVE_API_INTERNAL_URL ||
		process.env.ARCHIVE_API_URL ||
		'http://localhost:8001'
	).replace(/\/$/, '')
	const response = await fetch(`${archiveApiUrl}/stats`)
	if (!response.ok) return 0
	const stats = await response.json()
	return stats.summary?.[0]?.readings ?? 0
}

export async function getPublicTags() {
	const tags = await fetchJson<PublicApiTags>('/tags')
	return tags.data?.filter(Boolean) ?? []
}

export async function getPublicDevice(deviceId: string) {
	const device = await fetchJson<PublicApiDevice>(
		`/boxes/${encodeURIComponent(deviceId)}`,
	)

	return normalizeDevice(device)
}

export function registerPublicUser(input: {
	name: string
	email: string
	password: string
	language: 'de_DE' | 'en_US'
}) {
	return postPublicApi('/users/register', input)
}

export function signInPublicUser(input: {
	email: string
	password: string
}) {
	return postPublicApi('/users/sign-in', input)
}
