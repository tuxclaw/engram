module.exports = {
  apps: [
    {
      name: "engram-dashboard",
      script: "/home/thedev/clawd/.venv-memory/bin/python",
      args: "server.py",
      cwd: "/home/thedev/clawd/engram-dashboard",
      env: {
        ENGRAM_DB_PATH: "/home/thedev/clawd/engram/.engram-db",
        PYTHONUNBUFFERED: "1",
      },
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
