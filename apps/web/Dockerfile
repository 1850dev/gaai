FROM node:24.22.3-alpine3.23 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN ONNXRUNTIME_NODE_INSTALL=skip npm ci
COPY . .

ARG PUBLIC_API_BASE_URL
ARG PUBLIC_FIREBASE_API_KEY
ARG PUBLIC_FIREBASE_AUTH_DOMAIN
ARG PUBLIC_FIREBASE_PROJECT_ID
ARG PUBLIC_FIREBASE_APP_ID
ENV PUBLIC_API_BASE_URL=${PUBLIC_API_BASE_URL} \
    PUBLIC_FIREBASE_API_KEY=${PUBLIC_FIREBASE_API_KEY} \
    PUBLIC_FIREBASE_AUTH_DOMAIN=${PUBLIC_FIREBASE_AUTH_DOMAIN} \
    PUBLIC_FIREBASE_PROJECT_ID=${PUBLIC_FIREBASE_PROJECT_ID} \
    PUBLIC_FIREBASE_APP_ID=${PUBLIC_FIREBASE_APP_ID}

RUN npm run build && npm prune --omit=dev --ignore-scripts --no-audit --no-fund

FROM node:24.22.3-alpine3.23 AS runtime

ENV NODE_ENV=production \
    PORT=3000 \
    HOST=0.0.0.0
# The runtime starts Node directly; package managers are build-time tools.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /opt/yarn-v1.22.22 \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
        /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY --from=build --chown=app:app /app/build ./build
COPY --from=build --chown=app:app /app/package.json ./package.json
COPY --from=build --chown=app:app /app/node_modules ./node_modules
USER app
EXPOSE 3000
CMD ["node", "build"]
