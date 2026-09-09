# Use the official lightweight Python image
FROM python:3.14-slim

# Set the working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port Streamlit runs on (defaulting to 8080 for Cloud Run)
EXPOSE 8080

# Run the Streamlit app on container startup
CMD sh -c "streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0"
