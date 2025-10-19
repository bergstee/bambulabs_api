module.exports = {
  apps: [{
    name: 'bambulabs-monitor',
    script: 'venv/bin/python',
    args: 'examples/monitor_printers.py',
    cwd: '/opt/bambulabs-monitor',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      NODE_ENV: 'production',
      PYTHONPATH: '/opt/bambulabs-monitor',
      PYTHONUNBUFFERED: '1'
    },
    env_production: {
      NODE_ENV: 'production',
      PYTHONPATH: '/opt/bambulabs-monitor',
      PYTHONUNBUFFERED: '1'
    },
    error_file: '/var/log/bambulabs-monitor/error.log',
    out_file: '/var/log/bambulabs-monitor/access.log',
    log_file: '/var/log/bambulabs-monitor/combined.log',
    time: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    merge_logs: true,
    kill_timeout: 5000,
    wait_ready: false,
    listen_timeout: 10000,
    max_restarts: 10,
    min_uptime: '10s',
    restart_delay: 4000,
    // Exponential backoff for restarts
    exp_backoff_restart_delay: 100,
    max_restart_delay: 300000, // 5 minutes max delay
    // Watch for file changes (disabled by default)
    ignore_watch: [
      'node_modules',
      '.git',
      '*.log',
      '__pycache__',
      '.env'
    ]
  }]
};