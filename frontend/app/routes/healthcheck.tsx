// learn more: https://fly.io/docs/reference/configuration/#services-http_checks
import { type Route } from './+types/healthcheck'

export async function loader(_: Route.LoaderArgs) {
	try {
		const apiUrl = new URL('/stats', process.env.OSEM_API_URL)
		await fetch(apiUrl.toString(), {
			headers: { Accept: 'application/json' },
		}).then((r) => {
			if (!r.ok) return Promise.reject(r)
		})
		return new Response('OK')
	} catch (error: unknown) {
		console.log('healthcheck ❌', { error })
		return new Response('ERROR', { status: 500 })
	}
}
