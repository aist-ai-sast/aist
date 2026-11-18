export {};

declare global {
  function aistReadCookie(name: string): string | null;
  function aistCsrfToken(root?: ParentNode | null, preferCookie?: boolean): string;
}
