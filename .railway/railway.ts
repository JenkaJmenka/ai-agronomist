import { defineRailway, github, group, image, postgres, preserve, project, redis, service, volume } from "railway/iac";

export default defineRailway(() => {
  const Redis = redis("Redis", { region: "europe-west4-drams3a" });
  Redis.deploy = { startCommand: "/bin/sh -c \"rm -rf $RAILWAY_VOLUME_MOUNT_PATH/lost+found/ && exec docker-entrypoint.sh redis-server --requirepass $REDIS_PASSWORD --save 60 1 --dir $RAILWAY_VOLUME_MOUNT_PATH\"" };
  Redis.networking = { privateNetworkEndpoint: "redis", tcpProxies: { "6379": {} } };
  const aiAgronomistDb = postgres("ai-agronomist-db", { region: "europe-west4-drams3a" });
  aiAgronomistDb.networking = { privateNetworkEndpoint: "postgres" };
  const redisVolume = volume("redis-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "europe-west4-drams3a", sizeMB: 5000 });
  const rabbitmqVolumeXkOm = volume("rabbitmq-volume-XkOm", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 500 });
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "europe-west4-drams3a", sizeMB: 5000 });
  const aiAgronomistApi = service("ai-agronomist-api", {
    source: github("JenkaJmenka/ai-agronomist", { checkSuites: true, rootDirectory: "." }),
    build: { buildEnvironment: "V3", builder: "RAILPACK", watchPatterns: ["/src/**", "/alembic/**", "/alembic.ini", "/requirements.txt", "/docker/**", "/Dockerfile", "/.dockerignore", "/scripts/**", "/data/**", "/run_seeds.py"] },
    start: "/bin/sh -c \"exec /app/docker/start-api.sh\"",
    healthcheck: "/health",
    replicas: { "europe-west4-drams3a": 1 },
    env: { API_CREDENTIALS_JSON: preserve(), AUTH_ENABLED: preserve(), DATABASE_URL: preserve(), ENVIRONMENT: preserve(), LIVE_ANOMALY_THRESHOLD: preserve(), LIVE_AUTOSTART_ENABLED: preserve(), LOG_LEVEL: preserve(), MAX_PIPELINE_CACHE_SIZE: preserve(), MOCK_SCADA_ENABLED: preserve(), MOCK_SCADA_SCOPES: preserve(), PORT: preserve(), PROFILE_LOGGING_ENABLED: preserve(), RABBITMQ_URL: preserve(), REDIS_CACHE_EXPIRE_SECONDS: preserve(), REDIS_URL: preserve() },
  });
  const RabbitMQWebUI = service("RabbitMQ Web UI", {
    source: image("ghcr.io/brody192/railway-public-to-private-proxy"),
    replicas: { "sfo": 1 },
    networking: { privateNetworkEndpoint: "rabbitmq-web-ui" },
    env: { PROXY_HOST: preserve(), PROXY_PORT: preserve() },
  });
  const RabbitMQ = service("RabbitMQ", {
    source: image("rabbitmq:management"),
    start: "/bin/sh -c \"CONFIG_PATH=/etc; SYSTEM_FILE=hosts; echo 127.0.0.1 rabbitmq >> ${CONFIG_PATH}/${SYSTEM_FILE} && echo management.tcp.ip = :: >> /etc/rabbitmq/conf.d/10-defaults.conf && docker-entrypoint.sh rabbitmq-server\"",
    replicas: { "sfo": 1 },
    networking: { privateNetworkEndpoint: "rabbitmq", tcpProxies: { "5672": {} } },
    volumeMounts: { "/var/lib/rabbitmq": rabbitmqVolumeXkOm },
    env: { RABBITMQ_DEFAULT_PASS: preserve(), RABBITMQ_DEFAULT_USER: preserve(), RABBITMQ_NODENAME: preserve(), RABBITMQ_PRIVATE_URL: preserve(), RABBITMQ_URL: preserve() },
  });
  const web = service("web", {
    source: github("JenkaJmenka/ai-agronomist", { checkSuites: true, rootDirectory: "web" }),
    build: { buildEnvironment: "V3", builder: "RAILPACK", buildCommand: "pnpm build", watchPatterns: ["/web/**"] },
    start: "pnpm start --port ${PORT:-3000}",
    healthcheck: "/api/health",
    healthcheckTimeout: 120,
    // restartPolicyType is omitted on purpose: ON_FAILURE is Railway's default and
    // is stored as null, so setting it explicitly never converges. See AGENT_MEMORY.md.
    deploy: { restartPolicyMaxRetries: 3 },
    replicas: { "europe-west4-drams3a": 1 },
    networking: { serviceDomains: { "web-production-cd280.up.railway.app": {} } },
    env: { API_KEY: preserve(), API_URL: preserve(), AUTH_DATABASE_URL: preserve(), AUTH_RESEND_FROM: preserve(), AUTH_RESEND_KEY: preserve(), AUTH_SECRET: preserve(), AUTH_URL: preserve(), PROFILE_LOGGING_ENABLED: preserve() },
  });
  const aiAgronomistWorker = service("ai-agronomist-worker", {
    source: github("JenkaJmenka/ai-agronomist", { checkSuites: true }),
    build: { buildEnvironment: "V3", builder: "RAILPACK", watchPatterns: ["/src/**", "/alembic/**", "/alembic.ini", "/requirements.txt", "/docker/**", "/Dockerfile", "/.dockerignore", "/scripts/**", "/data/**", "/run_seeds.py"] },
    start: "/bin/sh -c \"exec /app/docker/start-worker.sh\"",
    replicas: { "europe-west4-drams3a": 1 },
    networking: { privateNetworkEndpoint: "ai-agronomist" },
    env: { DATABASE_URL: preserve(), LOG_LEVEL: preserve(), MAX_PIPELINE_CACHE_SIZE: preserve(), PROFILE_LOGGING_ENABLED: preserve(), RABBITMQ_URL: preserve(), UPLOAD_PORTION_ROWS: preserve() },
  });
  const RabbitMQ2 = group("RabbitMQ", [RabbitMQWebUI, RabbitMQ]);

  return project("ai-agronomist", {
    resources: [Redis, aiAgronomistApi, aiAgronomistDb, web, aiAgronomistWorker, redisVolume, rabbitmqVolumeXkOm, postgresVolume, RabbitMQ2],
  });
});
