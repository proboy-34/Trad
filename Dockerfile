# TradBot server image (used by Hugging Face Spaces, works on any Docker host)
FROM python:3.12-slim

# LightGBM needs the OpenMP runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face runs containers as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

# Dashboard on port 7860 (Hugging Face default). DASHBOARD_PASSWORD must be set as a secret.
ENV PORT=7860 TRADBOT_HOST=0.0.0.0
EXPOSE 7860
CMD ["python", "main.py", "run", "--server"]
