import * as dotenv from 'dotenv'
import { type Config } from 'drizzle-kit'
dotenv.config()

export default {
	schema: './app/db/schema/index.ts',
	out: './app/db/drizzle',
	connectionString: process.env.DATABASE_URL!,
} satisfies Config
