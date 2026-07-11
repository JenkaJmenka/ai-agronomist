This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

## Deploy on Railway (cutover P5)

The service is configured as code in `railway.json`; the pieces set in the
Railway dashboard are:

- **Root directory:** `web` (this folder — the repo root is the Python API).
- **Environment variables:**
  - `API_URL` — FastAPI internal URL, e.g. `http://<api-service>.railway.internal:8000`
    (IPv6 private networking; the API already binds `::`).
  - `API_KEY` — tenant API key injected server-side by the BFF. Never expose it
    as `NEXT_PUBLIC_*`.
- **Port:** Railway injects `PORT`; the start command binds it explicitly.

Cutover itself (switching the production hostname from Streamlit to this
service) is a manual routing change in Railway and stays a human decision —
rollback is flipping the hostname back to the still-warm Streamlit service.
