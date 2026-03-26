# Deploy guide

## Render

1. Push this folder to a GitHub repo.
2. In Render, create a new Web Service from that repo.
3. Use the included `render.yaml`, or set it manually:
   - Runtime: Docker
   - Health check path: `/`
4. Add environment variables:
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL=gpt-5`
5. Deploy.

## Railway

1. Push this folder to a GitHub repo.
2. In Railway, create a new project and deploy from that repo, or use the included Dockerfile.
3. Add environment variables:
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL=gpt-5`
4. Deploy.

## Notes

- This app processes uploaded videos on the server, so pick a plan that can handle video uploads.
- If you want uploads to survive restarts, attach persistent storage and move `uploads/` and `runs/` there.
