const DEFAULT_ARCHIVE_API_URL = 'http://localhost:8001'

export function getArchiveApiUrl() {
	return (globalThis.ENV?.ARCHIVE_API_URL || DEFAULT_ARCHIVE_API_URL).replace(
		/\/$/,
		'',
	)
}

export function archiveApiUrl(path: string) {
	return `${getArchiveApiUrl()}${path.startsWith('/') ? path : `/${path}`}`
}

export async function validateAOI(file: File) {
	const formData = new FormData()
	formData.append('file', file)

	const response = await fetch(archiveApiUrl('/aoi/validate'), {
		method: 'POST',
		body: formData,
	})

	const data = await response.json()

	if (!response.ok) {
		throw new Error(data.detail || 'Invalid area file.')
	}

	return data
}
