import { redirect } from "next/navigation";

/** Legacy Configuration route → Greenhouse Profile (preserve dashboard query). */
export default async function ConfigurationRedirectPage({
  searchParams,
}: {
  readonly searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === "string") q.set(key, value);
    else if (Array.isArray(value)) {
      for (const item of value) q.append(key, item);
    }
  }
  const qs = q.toString();
  redirect(qs ? `/greenhouse-profile?${qs}` : "/greenhouse-profile");
}
