# Use official Playwright image with all deps preinstalled
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python deps
RUN pip install --upgrade pip && pip install -r requirements.txt

# Run playwright install (install browser binaries)
RUN playwright install

# Expose the port used by your server
EXPOSE 10000

# Start your MCP server
CMD ["python", "server.py"]
