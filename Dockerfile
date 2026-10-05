# eFootball Champions League – runs anywhere Docker runs (Railway, Fly.io, Render, a VPS…)
FROM node:22-alpine
WORKDIR /app
COPY package.json server.js ./
COPY public ./public
ENV NODE_ENV=production PORT=3000 DB_FILE=/data/league.db TRUST_PROXY=1
# Mount a persistent volume at /data, or the league is lost when the container is replaced.
RUN mkdir -p /data
VOLUME /data
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- "http://127.0.0.1:${PORT:-3000}/api/health" || exit 1
CMD ["node", "server.js"]
