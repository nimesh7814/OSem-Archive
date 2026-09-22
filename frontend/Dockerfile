ARG NODE_VERSION=24

# base node image
FROM node:${NODE_VERSION}-slim AS base

# set for base and all layer that inherit from it
ENV NODE_ENV=production

# Update system and install some stuff
RUN apt-get update && apt-get install -y --no-install-recommends dumb-init

# Install all node_modules, including dev dependencies
FROM base AS deps

WORKDIR /myapp

COPY package.json package-lock.json ./
RUN npm ci --include=dev

# Setup production node_modules
FROM base AS production-deps

WORKDIR /myapp

COPY --from=deps /myapp/node_modules /myapp/node_modules
COPY package.json package-lock.json ./
RUN npm prune --omit=dev

# Build the app
FROM base AS build

WORKDIR /myapp

COPY --from=deps /myapp/node_modules /myapp/node_modules
COPY . .

RUN npm run build

# Finally, build the production image with minimal footprint
FROM base

WORKDIR /myapp

COPY --from=production-deps /myapp/node_modules /myapp/node_modules
COPY --from=build /myapp/build /myapp/build

# Not sure if we really need this or if we should move all our /public folder to /app/assets
COPY --from=build /myapp/public /myapp/public

COPY ./package.json ./package.json
COPY ./entrypoint.sh ./entrypoint.sh
COPY ./scripts/db/migrate.ts ./scripts/db/env-schema.ts ./scripts/db/
COPY ./app/db/drizzle ./app/db/drizzle
RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
