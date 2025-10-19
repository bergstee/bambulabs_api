#!/bin/bash

# Bambu Labs Printer Monitor - Ubuntu Deployment Script
# Run this script on your Ubuntu VM to install and configure the monitoring application

set -e

echo "🚀 Starting Bambu Labs Printer Monitor deployment..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
APP_DIR="/opt/bambulabs-monitor"
APP_USER="bambulabs"
LOG_DIR="/var/log/bambulabs-monitor"

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   echo -e "${RED}This script should not be run as root${NC}"
   echo "Please run as a regular user with sudo privileges"
   exit 1
fi

echo -e "${YELLOW}📦 Installing system dependencies...${NC}"

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and development tools
sudo apt install -y python3 python3-pip python3-venv python3-dev build-essential git curl

# Install PostgreSQL client tools
sudo apt install -y postgresql-client

# Install Node.js and npm
if ! command -v node &> /dev/null; then
    echo -e "${YELLOW}📥 Installing Node.js...${NC}"
    curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# Install PM2
if ! command -v pm2 &> /dev/null; then
    echo -e "${YELLOW}📥 Installing PM2...${NC}"
    sudo npm install -g pm2
fi

echo -e "${YELLOW}👤 Creating application user...${NC}"

# Create application user if it doesn't exist
if ! id "$APP_USER" &>/dev/null; then
    sudo useradd -r -s /bin/bash -d $APP_DIR $APP_USER
fi

echo -e "${YELLOW}📁 Setting up application directory...${NC}"

# Create application directory
sudo mkdir -p $APP_DIR
sudo chown $APP_USER:$APP_USER $APP_DIR

# Create log directory
sudo mkdir -p $LOG_DIR
sudo chown $APP_USER:$APP_USER $LOG_DIR

echo -e "${YELLOW}📋 Copying application files...${NC}"

# Copy application files (assuming script is run from the project directory)
sudo cp -r . $APP_DIR/
sudo chown -R $APP_USER:$APP_USER $APP_DIR

echo -e "${YELLOW}🐍 Setting up Python environment...${NC}"

# Switch to application user for Python setup
sudo -u $APP_USER bash << EOF
cd $APP_DIR
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
EOF

echo -e "${YELLOW}⚙️ Configuring environment...${NC}"

# Copy environment template if .env doesn't exist
if [[ ! -f $APP_DIR/.env ]]; then
    sudo cp $APP_DIR/deployment/.env.template $APP_DIR/.env
    sudo chown $APP_USER:$APP_USER $APP_DIR/.env
    sudo chmod 600 $APP_DIR/.env
    echo -e "${YELLOW}📝 Please edit $APP_DIR/.env with your database configuration${NC}"
fi

echo -e "${YELLOW}🔧 Setting up PM2...${NC}"

# Setup PM2 for the application user
sudo -u $APP_USER bash << EOF
cd $APP_DIR
pm2 start deployment/ecosystem.config.js
pm2 save
EOF

# Setup PM2 startup script
sudo env PATH=$PATH:/usr/bin /usr/lib/node_modules/pm2/bin/pm2 startup systemd -u $APP_USER --hp $APP_DIR

echo -e "${YELLOW}🏥 Setting up health monitoring...${NC}"

# Create health check script
sudo tee /usr/local/bin/bambulabs-monitor-health-check > /dev/null << 'EOF'
#!/bin/bash

# Check if PM2 process is running
PM2_STATUS=$(sudo -u bambulabs pm2 jlist | jq -r '.[] | select(.name=="bambulabs-monitor") | .pm2_env.status')

if [ "$PM2_STATUS" = "online" ]; then
    echo "Bambulabs Monitor is running"
    
    # Check if recent logs exist (activity in last 10 minutes)
    if find /var/log/bambulabs-monitor -name "*.log" -mmin -10 | grep -q .; then
        echo "Recent log activity detected"
        exit 0
    else
        echo "No recent log activity - service may be stuck"
        exit 1
    fi
else
    echo "Bambulabs Monitor is not running (status: $PM2_STATUS)"
    exit 1
fi
EOF

sudo chmod +x /usr/local/bin/bambulabs-monitor-health-check

# Install jq if not present (needed for health check)
if ! command -v jq &> /dev/null; then
    sudo apt install -y jq
fi

# Create systemd timer for health checks
sudo tee /etc/systemd/system/bambulabs-monitor-health-check.service > /dev/null << EOF
[Unit]
Description=Bambulabs Monitor Health Check
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/bambulabs-monitor-health-check
User=root
EOF

sudo tee /etc/systemd/system/bambulabs-monitor-health-check.timer > /dev/null << EOF
[Unit]
Description=Run Bambulabs Monitor Health Check every 5 minutes
Requires=bambulabs-monitor-health-check.service

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable bambulabs-monitor-health-check.timer
sudo systemctl start bambulabs-monitor-health-check.timer

echo -e "${YELLOW}🗄️ Database setup reminder...${NC}"
echo -e "${YELLOW}Please ensure your PostgreSQL database has the required tables:${NC}"
echo "- printers (with your printer configurations)"
echo "- printer_filaments"
echo "- printer_status_logs"
echo "- printer_job_history"

echo -e "${GREEN}✅ Deployment completed successfully!${NC}"
echo
echo -e "${YELLOW}📝 Next steps:${NC}"
echo "1. Edit database configuration: sudo nano $APP_DIR/.env"
echo "2. Ensure printers are configured in PostgreSQL database"
echo "3. Check service status: sudo -u $APP_USER pm2 status"
echo "4. View logs: sudo -u $APP_USER pm2 logs bambulabs-monitor"
echo "5. Monitor application: sudo -u $APP_USER pm2 monit"
echo
echo -e "${YELLOW}🔧 Management commands:${NC}"
echo "- Start: sudo -u $APP_USER pm2 start bambulabs-monitor"
echo "- Stop: sudo -u $APP_USER pm2 stop bambulabs-monitor"
echo "- Restart: sudo -u $APP_USER pm2 restart bambulabs-monitor"
echo "- View logs: sudo -u $APP_USER pm2 logs bambulabs-monitor"
echo "- Monitor: sudo -u $APP_USER pm2 monit"
echo
echo -e "${YELLOW}📊 Database commands:${NC}"
echo "- Test connection: psql -h \$DB_HOST -U \$DB_USER -d \$DB_NAME"
echo "- Check printers: SELECT * FROM printers;"
echo "- Check recent logs: SELECT * FROM printer_status_logs ORDER BY logged_at DESC LIMIT 10;"
echo
echo -e "${GREEN}🎉 Your Bambu Labs Printer Monitor is now running with PM2!${NC}"
echo -e "${YELLOW}📡 Monitor logs: tail -f $LOG_DIR/combined.log${NC}"