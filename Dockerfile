FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Build/deploy identity, threaded in by the deploy workflows (.git is
# dockerignored, so the running container can't read it otherwise). Placed
# after COPY so a new commit only rebuilds this tiny final layer.
ARG GIT_SHA=dev
ARG GIT_BRANCH=
ARG BUILD_TIME=
ENV GIT_SHA=$GIT_SHA
ENV GIT_BRANCH=$GIT_BRANCH
ENV BUILD_TIME=$BUILD_TIME
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
