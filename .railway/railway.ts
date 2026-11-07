import { defineRailway, github, postgres, preserve, project, service, volume } from "railway/iac";

// Singapore is the closest Railway region to India.
const REGION = "sin";
const REPO = "SuvinSss/Finance-Tracker-Zuuvi";

// Sized for Railway's Free plan ($1 credit/month, 0.5 GB volumes): both services sleep when idle.
export default defineRailway(() => {
  const Postgres = postgres("Postgres", { region: REGION });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  Postgres.deploy = { ...Postgres.deploy, sleepApplication: true };
  const postgresVolume = volume("postgres-volume", {
    alerts: { usage: { "100": {}, "80": {}, "95": {} } },
    allowOnlineResize: true,
    region: REGION,
    sizeMB: 500,
  });

  const web = service("web", {
    source: github(REPO, { branch: "main" }),
    regions: { [REGION]: 1 },
    preDeploy: "python manage.py migrate --noinput",
    start:
      "python manage.py collectstatic --noinput && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 1 --threads 2 --access-logfile -",
    healthcheck: "/login/",
    healthcheckTimeout: 120,
    deploy: { sleepApplication: true },
    env: {
      DATABASE_URL: Postgres.env.DATABASE_URL,
      DJANGO_SECRET_KEY: preserve(),
      DJANGO_DEBUG: "False",
      MEDIA_ROOT: "/app/media",
    },
    // Uploaded receipts live here so they survive redeploys.
    volumeMounts: {
      "/app/media": volume("media", { region: REGION, sizeMB: 500 }),
    },
  });

  return project("zuuvi-finance-tracker", {
    resources: [Postgres, postgresVolume, web],
  });
});
