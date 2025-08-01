# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install necessary system dependencies and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        python3-dev \
        antiword \
        libmagic1 \
        libreoffice \
        poppler-utils \
        zlib1g-dev \
        wget \
        tar \
        gcc \
        make \
        dpkg-dev && \
    rm -rf /var/lib/apt/lists/*

RUN wget https://www.rarlab.com/rar/unrar_5.2.5-0.1_amd64.deb  && \
    dpkg -i unrar_5.2.5-0.1_amd64.deb && \
    rm unrar_5.2.5-0.1_amd64.deb

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container
COPY . .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 20142 available to the world outside this container
EXPOSE 20142

# Run the application with auto-reload
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "20142", "--reload"]