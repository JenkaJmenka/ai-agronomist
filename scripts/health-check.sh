#!/bin/bash

echo "Checking Agronomist services health..."

# Check PostgreSQL
echo -n "PostgreSQL: "
if docker-compose exec -T postgres pg_isready -U postgres &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

# Check Jupyter
echo -n "Jupyter: "
if curl -s http://localhost:8888/lab &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

# Check TensorBoard
echo -n "TensorBoard: "
if curl -s http://localhost:6006 &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

# Check Adminer
echo -n "Adminer: "
if curl -s http://localhost:8080 &>/dev/null; then
    echo "✓ Healthy"
else
    echo "✗ Not responding"
fi

echo ""
echo "Full status:"
docker-compose ps
