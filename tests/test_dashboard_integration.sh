#!/bin/bash

# Quick test script for Dashboard

echo "🚀 Testing Lynx Memory Dashboard..."
echo ""

# Check if syll is installed
if ! command -v syll &> /dev/null; then
    echo "❌ syll not found. Installing..."
    pip install -e .
fi

# Create some test events
echo "📝 Creating test events..."
python test_dashboard.py

# Check if gateway is running
if curl -s http://localhost:18790/api/v1/status > /dev/null 2>&1; then
    echo "✅ Gateway is running"
else
    echo "⚠️  Gateway not running. Start it with:"
    echo "   syll gateway"
    echo ""
    echo "   Or with agents:"
    echo "   syll gateway --memory-agent --monitor-agent"
    exit 1
fi

# Test dashboard endpoints
echo ""
echo "🧪 Testing Dashboard API..."

echo -n "  /api/v1/dashboard/stats ... "
if curl -s http://localhost:18790/api/v1/dashboard/stats | grep -q "today_count"; then
    echo "✅"
else
    echo "❌"
fi

echo -n "  /api/v1/dashboard/timeline ... "
if curl -s http://localhost:18790/api/v1/dashboard/timeline | grep -q "\["; then
    echo "✅"
else
    echo "❌"
fi

echo -n "  /dashboard page ... "
if curl -s http://localhost:18790/dashboard | grep -q "Lynx Memory Dashboard"; then
    echo "✅"
else
    echo "❌"
fi

echo ""
echo "🎉 Dashboard is ready!"
echo "   Open: http://localhost:18790/dashboard"
echo ""
echo "💡 Tips:"
echo "   - Click on the heatmap to see day details"
echo "   - Use filters to view specific agent types"
echo "   - Enable agents with: syll gateway --memory-agent --monitor-agent"
