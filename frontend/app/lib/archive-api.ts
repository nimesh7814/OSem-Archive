const DEFAULT_API_URL = 'http://127.0.0.1:8001'

export function getArchiveApiUrl() {
	const configuredUrl =
		typeof window === 'undefined'
			? process.env.ARCHIVE_API_INTERNAL_URL ?? process.env.ARCHIVE_API_URL
			: window.ENV?.ARCHIVE_API_URL

	return (configuredUrl || DEFAULT_API_URL).replace(/\/+$/, '')
}

export function archiveApiUrl(path: string) {
	return `${getArchiveApiUrl()}${path.startsWith('/') ? path : `/${path}`}`
}

export async function validateAOI(file: File) {
	const formData = new FormData()
	formData.append("file", file)

	const response = await fetch(archiveApiUrl('/aoi/validate'), {
		method: "POST",
		body: formData,
	})

	const data = await response.json()

	if (!response.ok) {
		throw new Error(data.detail || "Invalid area file.")
	}

	return data
}
