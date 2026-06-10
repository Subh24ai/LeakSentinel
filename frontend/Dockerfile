# Build the Vite app, then serve the static bundle with nginx.
FROM node:20-slim AS build
WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
# In the container the SPA talks to the backend through nginx at /api (see
# nginx.conf), so there is no cross-origin call and no CORS dependency.
ENV VITE_API_BASE=/api
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
