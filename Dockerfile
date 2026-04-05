FROM python:3.11

# Node.js (>= 18) and basic build tools
RUN apt-get update \
  && apt-get install -y --no-install-recommends nodejs npm \
  && rm -rf /var/lib/apt/lists/*

# Copy uv from official image
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

WORKDIR /app

# Dependency manifests first (better layer cache)
COPY package.json package-lock.json ./
COPY frontend/package.json frontend/package-lock.json ./frontend/
COPY backend/pyproject.toml backend/uv.lock ./backend/

# Install Node and Python dependencies
RUN npm ci \
  && npm ci --prefix frontend \
  && cd backend && uv sync

# Application source
COPY . .

EXPOSE 3000 5001

# Dev mode: frontend + backend
CMD ["npm", "run", "dev"]
