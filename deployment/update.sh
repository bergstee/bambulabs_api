#!/bin/bash

# Bambu Labs Printer Monitor - Update Script
# Run this script to update the monitoring application

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

APP_DIR="/opt/bambulabs-monitor"
APP_USER="bambulabs"

echo -e "${YELLOW}🔄 Updating Bambu Labs Printer Monitor...${NC}"

# Check if running as the app user or with sudo
if [[ $USER != $APP_USER ]] && [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script should be run as $APP_USER user or with sudo${NC}"
   exit 1
fi

cd $APP_DIR

echo -e "${YELLOW}📥 Pulling latest changes...${NC}"
sudo -u $APP_USER git pull

echo -e "${YELLOW}📦 Updating dependencies...${NC}"
sudo -u $APP_USER bash << EOF
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
EOF

echo -e "${YELLOW}🔄 Reloading application...${NC}"
sudo -u $APP_USER pm2 reload bambulabs-monitor

echo -e "${YELLOW}💾 Saving PM2 configuration...${NC}"
sudo -u $APP_USER pm2 save

echo -e "${GREEN}✅ Update completed successfully!${NC}"
echo
echo -e "${YELLOW}📊 Current status:${NC}"
sudo -u $APP_USER pm2 status

echo
echo -e "${YELLOW}📝 To view logs:${NC}"
echo "sudo -u $APP_USER pm2 logs bambulabs-monitor"