FROM node:24-alpine AS dashboard
WORKDIR /dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .
COPY --from=dashboard /dashboard/dist dashboard/dist
EXPOSE 8787
CMD ["repomesh", "serve", "--container-loopback-publish"]
