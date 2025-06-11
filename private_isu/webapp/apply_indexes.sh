#!/bin/bash

# Apply database performance indexes for private_isu

# Set environment variables
source /Users/i.kato/isucon-kenshu/isucon-kenshu/env.sh

echo "Applying database indexes for performance optimization..."

# Apply the indexes
mysql -u ${ISUCONP_DB_USER} -h ${ISUCONP_DB_HOST} -p${ISUCONP_DB_PASSWORD} ${ISUCONP_DB_NAME} </Users/i.kato/isucon-kenshu/isucon-kenshu/private_isu/webapp/sql/add_indexes.sql

if [ $? -eq 0 ]; then
    echo "✅ Database indexes applied successfully!"
else
    echo "❌ Failed to apply database indexes"
    exit 1
fi

echo "Performance optimization complete!"
