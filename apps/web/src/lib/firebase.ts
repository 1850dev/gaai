import { browser } from '$app/env';
import {
  PUBLIC_FIREBASE_API_KEY,
  PUBLIC_FIREBASE_AUTH_DOMAIN,
  PUBLIC_FIREBASE_PROJECT_ID,
  PUBLIC_FIREBASE_APP_ID
} from '$app/env/public';
import { getApp, getApps, initializeApp, type FirebaseApp } from 'firebase/app';
import {
  GoogleAuthProvider,
  getAuth,
  signInWithPopup,
  signOut,
  type Auth,
  type User
} from 'firebase/auth';

function configuration(): Record<string, string> | null {
  const config = {
    apiKey: PUBLIC_FIREBASE_API_KEY,
    authDomain: PUBLIC_FIREBASE_AUTH_DOMAIN,
    projectId: PUBLIC_FIREBASE_PROJECT_ID,
    appId: PUBLIC_FIREBASE_APP_ID
  };
  return Object.values(config).every(Boolean) ? config : null;
}

export function firebaseConfigured(): boolean {
  return configuration() !== null;
}

export function firebaseApp(): FirebaseApp | null {
  if (!browser) return null;
  const config = configuration();
  if (!config) return null;
  return getApps().length ? getApp() : initializeApp(config);
}

export function firebaseAuth(): Auth | null {
  const app = firebaseApp();
  return app ? getAuth(app) : null;
}

export async function loginWithGoogle(): Promise<User> {
  const auth = firebaseAuth();
  if (!auth) throw new Error('Firebase public configuration is incomplete.');
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: 'select_account' });
  const credential = await signInWithPopup(auth, provider);
  return credential.user;
}

export async function logout(): Promise<void> {
  const auth = firebaseAuth();
  if (auth) await signOut(auth);
}
