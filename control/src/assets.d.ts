// CSS files imported as text (wrangler.jsonc "rules"): pages.ts inlines the shared palette.
declare module "*.css" {
  const text: string;
  export default text;
}
