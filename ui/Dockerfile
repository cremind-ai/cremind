# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies (npm ci + explicit platform dep for rollup on linux-musl)
RUN npm ci
RUN npm install --no-save @rollup/rollup-linux-x64-musl

# Copy source files
COPY . .

# Build the web application and server
RUN npm run web:build

# Production stage
FROM node:20-alpine

WORKDIR /app

# Copy package files for production dependencies
COPY package*.json ./

# Install only production dependencies
RUN npm ci --only=production

# Copy built files from builder
COPY --from=builder /app/dist-web ./dist-web
COPY --from=builder /app/dist-server ./dist-server

# Expose the port
EXPOSE 8000

# Set environment variable
ENV NODE_ENV=production

# Start the server
CMD ["node", "dist-server/index.js"]
