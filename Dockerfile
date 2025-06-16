# Use the correct version to match Playwright
FROM mcr.microsoft.com/playwright/python:v1.52.0-jammy

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python packages
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Playwright browser binaries
RUN playwright install

# Optional: expose your server port
EXPOSE 10000

# Start your MCP server
CMD ["python", "server.py"]
