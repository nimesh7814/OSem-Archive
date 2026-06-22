#!/bin/sh
set -ex

npx --yes tsx ./scripts/db/migrate.ts
npm run start
