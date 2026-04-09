const GLOBAL_TENANT_KEY = 'global_tenant_id';
const TENANT_SCOPE_EVENT = 'tenant_scope_changed';

export const getGlobalTenantId = () => localStorage.getItem(GLOBAL_TENANT_KEY) || '';

export const setGlobalTenantId = (tenantId) => {
  const value = tenantId || '';
  if (value) {
    localStorage.setItem(GLOBAL_TENANT_KEY, value);
  } else {
    localStorage.removeItem(GLOBAL_TENANT_KEY);
  }
  window.dispatchEvent(new CustomEvent(TENANT_SCOPE_EVENT, { detail: { tenantId: value } }));
};

export const subscribeGlobalTenant = (callback) => {
  // Emit current value immediately to avoid missing a prior tenant switch event
  // (common when page component mounts after layout has already set tenant).
  callback(getGlobalTenantId());
  const handler = (evt) => callback(evt?.detail?.tenantId || '');
  window.addEventListener(TENANT_SCOPE_EVENT, handler);
  return () => window.removeEventListener(TENANT_SCOPE_EVENT, handler);
};
