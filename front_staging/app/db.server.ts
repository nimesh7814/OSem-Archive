import { drizzle, type PostgresJsDatabase } from 'drizzle-orm/postgres-js'
import postgres, { type Sql } from 'postgres'
import invariant from 'tiny-invariant'
import * as schema from './db/schema'

type DbClients = {
	drizzle: PostgresJsDatabase<typeof schema>
	pg: Sql<any>
}

declare global {
	var __db__: DbClients | undefined
}

let productionDb: DbClients | undefined

function getDb() {
	if (process.env.NODE_ENV === 'production') {
		productionDb ??= initClient()
		return productionDb
	}

	global.__db__ ??= initClient()
	return global.__db__
}

function initClient() {
	const { DATABASE_URL } = process.env
	invariant(typeof DATABASE_URL === 'string', 'DATABASE_URL env var not set')

	const databaseUrl = new URL(DATABASE_URL)
	console.log(`🔌 setting up drizzle client to ${databaseUrl.host}`)

	const rawPg = postgres(DATABASE_URL, {
		ssl: process.env.PG_CLIENT_SSL === 'true' ? true : false,
	})

	const drizzleDb = drizzle(rawPg, { schema })

	return { drizzle: drizzleDb, pg: rawPg }
}

function bindIfFunction<T>(value: T, target: unknown) {
	return typeof value === 'function' ? value.bind(target) : value
}

const drizzleClient = new Proxy({} as PostgresJsDatabase<typeof schema>, {
	get(_target, prop, receiver) {
		const client = getDb().drizzle
		return bindIfFunction(Reflect.get(client, prop, receiver), client)
	},
})

const pg = new Proxy((() => {}) as unknown as Sql<any>, {
	get(_target, prop, receiver) {
		const client = getDb().pg
		return bindIfFunction(Reflect.get(client, prop, receiver), client)
	},
	apply(_target, thisArg, argArray) {
		return Reflect.apply(getDb().pg as any, thisArg, argArray)
	},
})

export { drizzleClient, pg }
