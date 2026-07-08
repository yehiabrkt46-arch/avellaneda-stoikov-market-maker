// pm2 process file for the 7-day paper run on the VPS (Linux).
// Adjust cwd to the clone location before `pm2 start deploy/ecosystem.config.js`.
module.exports = {
  apps: [
    {
      name: "mm-bot-paper",
      cwd: "/opt/mm-bot",
      script: ".venv/bin/python",
      args: "run_paper.py",
      interpreter: "none",
      autorestart: true,
      max_restarts: 100,
      restart_delay: 5000,
      out_file: "logs/paper.out.log",
      error_file: "logs/paper.err.log",
      merge_logs: true,
      time: true,
    },
  ],
};
