import { defineRailway, github, postgres, preserve, project, service, volume } from "railway/iac";

// Singapore is the closest Railway region to India.
const REGION = "sin";

export default defineRailway(() => {
  const Postgres = postgres("Postgres", { region: REGION });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const postgresVolume = volume("postgres-volume", {
    alerts: { usage: { "100": {}, "80": {}, "95": {} } },
    allowOnlineResize: true,
    region: REGION,
    sizeMB: 5000,
  });

  const web = service("web", {
    source: github("winsportsapp/Finance-Tracker-Zuuvi", { branch: "main" }),
    regions: { [REGION]: 1 },
    preDeploy: "python manage.py migrate --noinput",
    start:
      "python manage.py collectstatic --noinput && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --access-logfile -",
    healthcheck: "/login/",
    healthcheckTimeout: 120,
    env: {
      DATABASE_URL: Postgres.env.DATABASE_URL,
      DJANGO_SECRET_KEY: preserve(),
      DJANGO_DEBUG: "False",
      MEDIA_ROOT: "/app/media",
    },
    // Uploaded receipts live here so they survive redeploys.
    volumeMounts: {
      "/app/media": volume("media", { region: REGION, sizeMB: 1024 }),
    },
  });

  return project("zuuvi-finance-tracker", {
    resources: [Postgres, postgresVolume, web],
  });
});
