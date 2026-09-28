"use client";

import React, { Suspense, useEffect, useRef, useState } from "react";
import Script from "next/script";
import { useRouter, useSearchParams } from "next/navigation";
import { L } from "@/lib/labels";
import { createSession, getAuthErrorMessage } from "@/lib/api/auth";
import { useAuth } from "@/lib/auth/AuthProvider";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: any) => void;
          renderButton: (parent: HTMLElement, options: any) => void;
        };
      };
    };
  }
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = searchParams.get("next") || "/cases";
  const urlError = searchParams.get("error");

  const { me, refreshMe } = useAuth();
  const [errorMsg, setErrorMsg] = useState<string | null>(
    urlError ? getAuthErrorMessage(urlError) : null
  );
  const [loading, setLoading] = useState<boolean>(false);
  const googleBtnRef = useRef<HTMLDivElement>(null);

  const isMockMode = process.env.NEXT_PUBLIC_API_MOCK === "1";
  const googleClientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "";
  const domainHint = process.env.NEXT_PUBLIC_AUTH_DOMAIN_HINT || "";

  useEffect(() => {
    if (me) {
      router.replace(nextPath);
    }
  }, [me, nextPath, router]);

  const handleCredentialResponse = async (response: { credential: string }) => {
    try {
      setLoading(true);
      setErrorMsg(null);
      await createSession(response.credential);
      await refreshMe();
      router.replace(nextPath);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setLoading(false);
    }
  };

  const handleMockSignIn = async () => {
    try {
      setLoading(true);
      setErrorMsg(null);
      await createSession("mock_credential_token");
      await refreshMe();
      router.replace(nextPath);
    } catch (err: any) {
      setErrorMsg(getAuthErrorMessage(err?.message));
    } finally {
      setLoading(false);
    }
  };

  const initGsi = () => {
    if (
      !isMockMode &&
      googleClientId &&
      window.google?.accounts?.id &&
      googleBtnRef.current
    ) {
      window.google.accounts.id.initialize({
        client_id: googleClientId,
        callback: handleCredentialResponse,
      });
      googleBtnRef.current.innerHTML = "";
      window.google.accounts.id.renderButton(googleBtnRef.current, {
        theme: "outline",
        size: "large",
        width: 280,
      });
    }
  };

  return (
    <div className="min-h-full flex items-center justify-center p-4 bg-slate-900">
      {!isMockMode && (
        <Script
          src="https://accounts.google.com/gsi/client"
          strategy="afterInteractive"
          onLoad={initGsi}
        />
      )}
      <div className="w-full max-w-md bg-slate-800 border border-slate-700 rounded-xl shadow-2xl p-8 space-y-6">
        <div className="text-center space-y-2">
          <div className="inline-flex w-12 h-12 bg-sky-500 rounded-xl items-center justify-center font-bold text-slate-900 text-lg shadow">
            {L.heading.appLogo}
          </div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            {L.heading.appTitle}
          </h1>
          <p className="text-sm text-sky-400 font-mono">
            {L.heading.loginSubtitle}
          </p>
        </div>

        {errorMsg && (
          <div className="p-3 text-sm bg-rose-950/80 border border-rose-800 text-rose-300 rounded-lg">
            {errorMsg}
          </div>
        )}

        <div className="pt-2 flex flex-col items-center space-y-4">
          {isMockMode ? (
            <button
              type="button"
              onClick={handleMockSignIn}
              disabled={loading}
              className="w-full py-2.5 px-4 bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white font-medium rounded-lg shadow transition duration-150 flex items-center justify-center space-x-2"
            >
              <span>{loading ? L.status.processing : L.action.mockSignIn}</span>
            </button>
          ) : (
            <div
              ref={googleBtnRef}
              className="min-h-[44px] flex items-center justify-center"
            />
          )}

          {domainHint ? (
            <p className="text-xs text-slate-400 text-center">
              {L.fmt.domainAccount(domainHint)}
            </p>
          ) : (
            <p className="text-xs text-slate-400 text-center">
              {L.help.domainHint}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="min-h-full bg-slate-900" />}>
      <LoginForm />
    </Suspense>
  );
}
