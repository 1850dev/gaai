import { defineEnvVars } from '@sveltejs/kit/env';

const publicFirebaseValue = {
  public: true,
  schema: (value: string | undefined) => value ?? ''
};

export const variables = defineEnvVars({
  PUBLIC_API_BASE_URL: { public: true, schema: (value) => value || 'http://localhost:8080' },
  PUBLIC_FIREBASE_API_KEY: publicFirebaseValue,
  PUBLIC_FIREBASE_AUTH_DOMAIN: publicFirebaseValue,
  PUBLIC_FIREBASE_PROJECT_ID: publicFirebaseValue,
  PUBLIC_FIREBASE_APP_ID: publicFirebaseValue
});
