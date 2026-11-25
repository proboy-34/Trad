# TradBot server image - works on any Docker host / VPS.
# Run with:  docker run -d -p 8050:7860 -e DASHBOARD_PASSWORD=... -v tradbot-data:/home/user/app/data <image>
FROM python:3.12-slim

# LightGBM needs the OpenMP runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt \
    && (pip install --no-cache-dir --user -r requirements-optional.txt || true)

COPY --chown=user . .

# Dashboard port 7860. DASHBOARD_PASSWORD must be set, or the dashboard stays private.
ENV PORT=7860 TRADBOT_HOST=0.0.0.0
EXPOSE 7860
CMD ["python", "main.py", "run", "--server"]
