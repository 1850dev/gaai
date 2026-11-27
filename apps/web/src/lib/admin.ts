import type { User } from 'firebase/auth';
import { request } from './api';

export interface RegistrationDefinition {
  name: string;
  ga4: {
    property_id: string;
    display_name: string;
    auth_mode: 'USER_OAUTH' | 'SERVICE_ACCOUNT';
    timezone: string;
    currency_code: string;
    stream_scopes: { stream_id: string; platform: 'web' | 'android' | 'ios' }[];
  };
  data_access_profile_id?: string | null;
}
export interface RegisteredSource extends RegistrationDefinition {
  id: string;
  company_id: string;
  enabled: boolean;
  semantic_version: string;
}
export interface Registration {
  source_set: RegisteredSource;
  status: 'draft' | 'verified' | 'active' | 'disabled';
  revision: number;
  legacy_source_hash: string | null;
}
export interface InventoryItem {
  source_set: RegisteredSource;
  registration: Registration | null;
  source_hash: string;
  grants_hash: string | null;
  grants_truncated: boolean;
  grants: { id: string; user_id: string; enabled: boolean }[];
}
export interface Inventory {
  items: InventoryItem[];
  next_cursor: string | null;
}
export const registrationPath = (company: string, source?: string) =>
  `/api/admin/companies/${encodeURIComponent(company)}/data-source-registrations${source ? '/' + encodeURIComponent(source) : ''}`;
export const adminApi = {
  read: (user: User, company: string, source: string) =>
    request<Registration>(user, registrationPath(company, source)),
  companies: (user: User) => request<{ id: string; name: string }[]>(user, '/api/admin/companies'),
  inventory: (user: User, company: string, after = '') =>
    request<Inventory>(
      user,
      `/api/admin/companies/${encodeURIComponent(company)}/data-source-inventory?after=${encodeURIComponent(after)}`
    ),
  write: (user: User, company: string, source: string | undefined, action: string, body: unknown) =>
    request<Registration>(user, registrationPath(company, source) + (action ? '/' + action : ''), {
      method: action === 'grant' ? 'PUT' : 'POST',
      body: JSON.stringify(body)
    })
};
