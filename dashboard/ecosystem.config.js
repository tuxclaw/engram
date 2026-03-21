const path = require("path");
const rootDir = path.resolve(__dirname, "..");

module.exports = {
  apps: [{
    name: "engram-dashboard",
    script: path.join(rootDir, ".venv-memory", "bin", "python"),
    args: "-m uvicorn server:app --host 0.0.0.0 --port 3460",
    cwd: __dirname,
    env: {
      ENGRAM_DB_PATH: path.join(rootDir, ".engram-db"),
    }
  }]
};
