#!/bin/bash

# Bambu Labs Printer Monitor - Backup Script
# Creates a backup of configuration and data

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

APP_DIR="/opt/bambulabs-monitor"
BACKUP_DIR="/opt/bambulabs-monitor-backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="bambulabs-monitor-backup-$DATE.tar.gz"

echo -e "${YELLOW}💾 Creating backup of Bambu Labs Printer Monitor...${NC}"

# Create backup directory
sudo mkdir -p $BACKUP_DIR

# Create backup
echo -e "${YELLOW}📦 Creating archive...${NC}"
sudo tar -czf "$BACKUP_DIR/$BACKUP_FILE" \
    -C $APP_DIR \
    .env \
    deployment/ \
    examples/monitor_printers.log \
    --exclude=venv \
    --exclude=.git \
    --exclude=__pycache__ \
    --exclude=*.pyc \
    --exclude=node_modules \
    --exclude=.pm2/logs

# Save PM2 configuration
echo -e "${YELLOW}💾 Backing up PM2 configuration...${NC}"
sudo -u bambulabs pm2 save

echo -e "${GREEN}✅ Backup created: $BACKUP_DIR/$BACKUP_FILE${NC}"

# Clean up old backups (keep last 7 days)
echo -e "${YELLOW}🧹 Cleaning up old backups...${NC}"
sudo find $BACKUP_DIR -name "bambulabs-monitor-backup-*.tar.gz" -mtime +7 -delete

echo -e "${GREEN}🎉 Backup completed successfully!${NC}"
echo
echo -e "${YELLOW}📂 Backup location: $BACKUP_DIR/$BACKUP_FILE${NC}"
echo -e "${YELLOW}📊 Backup size: $(sudo du -h "$BACKUP_DIR/$BACKUP_FILE" | cut -f1)${NC}"

# Optional: Backup database
echo -e "${YELLOW}💡 Don't forget to backup your PostgreSQL database separately:${NC}"
echo "pg_dump -h \$DB_HOST -U \$DB_USER \$DB_NAME > db_backup_$DATE.sql"