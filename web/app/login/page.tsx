import { LoginView } from "@/features/auth/LoginView";

interface LoginPageProps {
  readonly searchParams: Promise<{ callbackUrl?: string }>;
}

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const { callbackUrl } = await searchParams;
  return <LoginView callbackUrl={callbackUrl} />;
}
