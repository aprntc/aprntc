// Minimal inline icon set (stroke, currentColor) — no icon-font dependency.
// Size via className when provided, else a default; we don't combine both sizes
// (Tailwind can't reliably override conflicting h-/w- classes from string order).
type P = { className?: string };
const S = ({ children, className }: P & { children: any }) => (
  <svg className={className ?? "h-[18px] w-[18px]"} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    {children}
  </svg>
);

export const IconGate = (p: P) => (<S {...p}><path d="M5 21V5a2 2 0 012-2h10a2 2 0 012 2v16" /><path d="M3 21h18" /><path d="M9 7h6M9 11h6" /></S>);
export const IconBranch = (p: P) => (<S {...p}><circle cx="6" cy="6" r="2.5" /><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="8" r="2.5" /><path d="M6 8.5v7M8.4 7.2A6 6 0 0115.6 9" /></S>);
export const IconTrace = (p: P) => (<S {...p}><path d="M4 6h16M4 12h16M4 18h10" /></S>);
export const IconBook = (p: P) => (<S {...p}><path d="M4 5a2 2 0 012-2h12v18H6a2 2 0 01-2-2V5z" /><path d="M8 7h6M8 11h6" /></S>);
export const IconSun = (p: P) => (<S {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19" /></S>);
export const IconMoon = (p: P) => (<S {...p}><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" /></S>);
export const IconCheck = (p: P) => (<S {...p}><path d="M5 12l4 4L19 6" /></S>);
export const IconX = (p: P) => (<S {...p}><path d="M6 6l12 12M18 6L6 18" /></S>);
export const IconShield = (p: P) => (<S {...p}><path d="M12 3l8 3v6c0 4.5-3.2 7.7-8 9-4.8-1.3-8-4.5-8-9V6l8-3z" /><path d="M9 12l2 2 4-4" /></S>);
export const IconPlus = (p: P) => (<S {...p}><path d="M12 5v14M5 12h14" /></S>);
export const IconRocket = (p: P) => (<S {...p}><path d="M5 15c-1.5 1.5-2 5-2 5s3.5-.5 5-2" /><path d="M9 11a8 8 0 016-6 8 8 0 01-6 6z" /><path d="M14.5 9.5L13 15l-2-2 .5-3" /></S>);
export const IconUndo = (p: P) => (<S {...p}><path d="M9 14L4 9l5-5" /><path d="M4 9h11a5 5 0 010 10h-3" /></S>);
export const IconArrow = (p: P) => (<S {...p}><path d="M5 12h14M13 6l6 6-6 6" /></S>);
export const IconSearch = (p: P) => (<S {...p}><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4" /></S>);
export const IconDot = (p: P) => (<S {...p}><circle cx="12" cy="12" r="3" fill="currentColor" /></S>);
export const IconPlay = (p: P) => (<S {...p}><path d="M6 4l14 8-14 8V4z" /></S>);
export const IconSparkle = (p: P) => (<S {...p}><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" /></S>);
export const IconBolt = (p: P) => (<S {...p}><path d="M13 3L4 14h7l-1 7 9-11h-7l1-7z" /></S>);
export const IconThumbUp = (p: P) => (<S {...p}><path d="M7 11v9H4a1 1 0 01-1-1v-7a1 1 0 011-1h3z" /><path d="M7 11l4-7a2 2 0 012 2v3h5a2 2 0 012 2.3l-1.2 6A2 2 0 0118.8 20H7" /></S>);
export const IconThumbDown = (p: P) => (<S {...p}><path d="M17 13V4h3a1 1 0 011 1v7a1 1 0 01-1 1h-3z" /><path d="M17 13l-4 7a2 2 0 01-2-2v-3H6a2 2 0 01-2-2.3l1.2-6A2 2 0 015.2 4H17" /></S>);
export const IconUsers = (p: P) => (<S {...p}><circle cx="9" cy="8" r="3.5" /><path d="M3 20a6 6 0 0112 0" /><circle cx="17" cy="9" r="2.5" /><path d="M15 14h2a4 4 0 014 4" /></S>);
export const IconQuote = (p: P) => (<S {...p}><path d="M7 7h4v4H7zM13 7h4v4h-4z" /><path d="M7 11c0 2 1 3 3 3M13 11c0 2 1 3 3 3" /></S>);
