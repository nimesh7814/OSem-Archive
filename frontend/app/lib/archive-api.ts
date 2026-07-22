const API_URL = "http://127.0.0.1:8001"

export async function validateAOI(file: File) {
	const formData = new FormData()
	formData.append("file", file)

	const response = await fetch(`${API_URL}/aoi/validate`, {
		method: "POST",
		body: formData,
	})

	const data = await response.json()

	if (!response.ok) {
		throw new Error(data.detail || "Invalid area file.")
	}

	return data
}