# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install system dependencies (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy poetry files if using poetry, else requirements.txt
COPY poetry.lock pyproject.toml requirements.txt ./

# Install dependencies
RUN pip install --upgrade pip \
    && if [ -f "poetry.lock" ]; then pip install poetry && poetry install --no-root; fi \
    && if [ -f "requirements.txt" ]; then pip install -r requirements.txt; fi

# Copy the rest of the application code
COPY . .

# Expose the port (adjust if your app uses a different port)
EXPOSE 5000

# Set the default command to run the Flask app
CMD ["python", "-m", "evse_controller.app"]
