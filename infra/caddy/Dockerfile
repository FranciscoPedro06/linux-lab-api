# syntax=docker/dockerfile:1

# Edge image: Caddy with this repository's configuration and the frontend built from a
# linux-lab-web tree, passed as the named build context "web". Built by
# infra/release/build.sh from clean archives of both repositories:
#
#   docker build --build-context web=<web tree> -f infra/caddy/Dockerfile infra/caddy

FROM node:22.23.4-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /web
COPY --from=web package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --from=web . .
RUN npm run build

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
COPY Caddyfile linuxlab.caddy /etc/caddy/
COPY --from=build /web/dist /srv/web
