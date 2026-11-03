// A few icons drawn after Lucide (ISC, https://lucide.dev): 24×24, stroke 2, currentColor. Inline SVG, no font.
import type * as React from "react";

type IconProps = React.SVGProps<SVGSVGElement>;

function icon(name: string, children: React.ReactNode) {
  const Icon = (props: IconProps) => (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      width={16}
      height={16}
      {...props}
    >
      {children}
    </svg>
  );
  Icon.displayName = name;
  return Icon;
}

export const ArrowLeft = icon("ArrowLeft", <path d="m12 19-7-7 7-7M19 12H5" />);
export const Stop = icon("Stop", <rect width="14" height="14" x="5" y="5" rx="2" />);
export const Plus = icon("Plus", <path d="M5 12h14M12 5v14" />);
export const ChevronRight = icon("ChevronRight", <path d="m9 18 6-6-6-6" />);
export const Columns = icon(
  "Columns",
  <>
    <rect width="18" height="18" x="3" y="3" rx="2" />
    <path d="M12 3v18" />
  </>,
);
export const Close = icon("Close", <path d="M18 6 6 18M6 6l12 12" />);
export const FileSearch = icon(
  "FileSearch",
  <>
    <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    <path d="M4.268 21a2 2 0 0 0 1.727 1H18a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v3" />
    <circle cx="5" cy="14" r="3" />
    <path d="m9 18-1.5-1.5" />
  </>,
);
export const Pencil = icon(
  "Pencil",
  <path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" />,
);
export const ListTodo = icon(
  "ListTodo",
  <>
    <rect x="3" y="5" width="6" height="6" rx="1" />
    <path d="m3 17 2 2 4-4M13 6h8M13 12h8M13 18h8" />
  </>,
);
export const Bot = icon(
  "Bot",
  <>
    <path d="M12 8V4H8" />
    <rect width="16" height="12" x="4" y="8" rx="2" />
    <path d="M2 14h2M20 14h2M15 13v2M9 13v2" />
  </>,
);
export const Question = icon(
  "Question",
  <>
    <circle cx="12" cy="12" r="10" />
    <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01" />
  </>,
);
export const Terminal = icon("Terminal", <path d="m4 17 6-6-6-6M12 19h8" />);
export const Check = icon("Check", <path d="M20 6 9 17l-5-5" />);
export const Circle = icon("Circle", <circle cx="12" cy="12" r="9" />);
export const Dot = icon("Dot", <circle cx="12" cy="12" r="4" fill="currentColor" />);
export const LogOut = icon("LogOut", <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />);
export const Flag = icon("Flag", <path d="M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c3 0 5 2 7.333 2q2 0 3.067-.8A1 1 0 0 1 20 4v10a1 1 0 0 1-.4.8A6 6 0 0 1 16 16c-3 0-5-2-8-2a6 6 0 0 0-4 1.528" />);
export const Package = icon(
  "Package",
  <>
    <path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z" />
    <path d="M12 22V12M3.29 7 12 12l8.71-5" />
  </>,
);
export const Pin = icon(
  "Pin",
  <>
    <path d="M12 17v5" />
    <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />
  </>,
);
export const ChevronDown = icon("ChevronDown", <path d="m6 9 6 6 6-6" />);
export const MessageQuestion = icon(
  "MessageQuestion",
  <>
    <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
    <path d="M9.09 9a3 3 0 0 1 5.82 1c0 2-3 3-3 3M12 17h.01" />
  </>,
);
export const FileIcon = icon(
  "FileIcon",
  <>
    <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
    <path d="M14 2v4a2 2 0 0 0 2 2h4" />
  </>,
);
export const Mail = icon(
  "Mail",
  <>
    <rect width="20" height="16" x="2" y="4" rx="2" />
    <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" />
  </>,
);
export const Clock = icon(
  "Clock",
  <>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </>,
);
export const MessageCircle = icon("MessageCircle", <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />);
export const More = icon(
  "More",
  <>
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
    <circle cx="5" cy="12" r="1" />
  </>,
);
export const Info = icon(
  "Info",
  <>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 16v-4M12 8h.01" />
  </>,
);
export const FileDiff = icon(
  "FileDiff",
  <>
    <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
    <path d="M9 10h6M12 7v6M9 17h6" />
  </>,
);
export const Eye = icon(
  "Eye",
  <>
    <path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0" />
    <circle cx="12" cy="12" r="3" />
  </>,
);
export const CornerDownRight = icon(
  "CornerDownRight",
  <>
    <path d="m15 10 5 5-5 5" />
    <path d="M4 4v7a4 4 0 0 0 4 4h12" />
  </>,
);
export const Database = icon(
  "Database",
  <>
    <ellipse cx="12" cy="5" rx="9" ry="3" />
    <path d="M3 5v14a9 3 0 0 0 18 0V5" />
    <path d="M3 12a9 3 0 0 0 18 0" />
  </>,
);
export const Star = icon(
  "Star",
  <path d="M11.5 2.3a.5.5 0 0 1 .9 0l2.3 4.7a2 2 0 0 0 1.6 1.1l5.2.8a.5.5 0 0 1 .3.9l-3.8 3.7a2 2 0 0 0-.6 1.8l.9 5.2a.5.5 0 0 1-.8.6l-4.6-2.5a2 2 0 0 0-2 0l-4.6 2.5a.5.5 0 0 1-.8-.6l.9-5.2a2 2 0 0 0-.6-1.8L2.3 9.8a.5.5 0 0 1 .3-.9l5.2-.8A2 2 0 0 0 9.4 7z" />,
);
export const GitBranch = icon(
  "GitBranch",
  <>
    <circle cx="6" cy="6" r="3" />
    <circle cx="18" cy="6" r="3" />
    <circle cx="6" cy="18" r="3" />
    <path d="M6 9v6M18 9a9 9 0 0 1-9 9" />
  </>,
);
export const Home = icon(
  "Home",
  <>
    <path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8" />
    <path d="M3 10a2 2 0 0 1 .7-1.5l7-6a2 2 0 0 1 2.6 0l7 6a2 2 0 0 1 .7 1.5v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </>,
);
export const List = icon("List", <path d="M3 12h.01M3 18h.01M3 6h.01M8 12h13M8 18h13M8 6h13" />);
export const Grid = icon(
  "Grid",
  <>
    <rect width="7" height="7" x="3" y="3" rx="1" />
    <rect width="7" height="7" x="14" y="3" rx="1" />
    <rect width="7" height="7" x="14" y="14" rx="1" />
    <rect width="7" height="7" x="3" y="14" rx="1" />
  </>,
);
export const AppWindow = icon(
  "AppWindow",
  <>
    <rect x="2" y="4" width="20" height="16" rx="2" />
    <path d="M10 4v4M2 8h20M6 4v4" />
  </>,
);
export const Book = icon(
  "Book",
  <>
    <path d="M12 7v14" />
    <path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z" />
  </>,
);
export const Mic = icon(
  "Mic",
  <>
    <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
    <path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3" />
  </>,
);
export const ImageIcon = icon(
  "ImageIcon",
  <>
    <rect width="18" height="18" x="3" y="3" rx="2" />
    <circle cx="9" cy="9" r="2" />
    <path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
  </>,
);
export const ArrowUp = icon("ArrowUp", <path d="m5 12 7-7 7 7M12 19V5" />);
export const Camera = icon(
  "Camera",
  <>
    <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z" />
    <circle cx="12" cy="13" r="3" />
  </>,
);
export const Layers = icon(
  "Layers",
  <>
    <path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z" />
    <path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65M22 12.65l-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65" />
  </>,
);
export const Globe = icon(
  "Globe",
  <>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20M2 12h20" />
  </>,
);
export const Panel = icon(
  "Panel",
  <>
    <rect width="18" height="18" x="3" y="3" rx="2" />
    <path d="M15 3v18" />
  </>,
);
export const AlertCircle = icon(
  "AlertCircle",
  <>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 8v4M12 16h.01" />
  </>,
);
export const Plug = icon("Plug", <path d="M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z" />);
export const GitPullRequest = icon(
  "GitPullRequest",
  <>
    <circle cx="18" cy="18" r="3" />
    <circle cx="6" cy="6" r="3" />
    <path d="M13 6h3a2 2 0 0 1 2 2v7M6 9v12" />
  </>,
);
export const Chart = icon("Chart", <path d="M3 3v16a2 2 0 0 0 2 2h16M7 16l4-4 3 3 5-6" />);
export const Ban = icon("Ban", <><circle cx="12" cy="12" r="9" /><path d="m5.7 5.7 12.6 12.6" /></>);
export const Archive = icon(
  "Archive",
  <>
    <rect width="20" height="5" x="2" y="3" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8M10 12h4" />
  </>,
);
export const ArrowUpRight = icon("ArrowUpRight", <path d="M7 17 17 7M7 7h10v10" />);
export const Briefcase = icon(
  "Briefcase",
  <>
    <path d="M16 20V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
    <rect width="20" height="14" x="2" y="6" rx="2" />
  </>,
);
export const Plane = icon(
  "Plane",
  <path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z" />,
);
export const Luggage = icon(
  "Luggage",
  <>
    <path d="M6 20a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2" />
    <path d="M8 18V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v14M10 20h4" />
    <circle cx="16" cy="20" r="2" />
    <circle cx="8" cy="20" r="2" />
  </>,
);
export const CreditCard = icon(
  "CreditCard",
  <>
    <rect width="20" height="14" x="2" y="5" rx="2" />
    <path d="M2 10h20" />
  </>,
);
export const Stethoscope = icon(
  "Stethoscope",
  <>
    <path d="M11 2v2M5 2v2M5 3H4a2 2 0 0 0-2 2v4a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1M8 15a6 6 0 0 0 12 0v-3" />
    <circle cx="20" cy="10" r="2" />
  </>,
);
export const Megaphone = icon(
  "Megaphone",
  <path d="m3 11 18-5v12L3 14v-3zM11.6 16.8a3 3 0 1 1-5.8-1.6" />,
);
export const Target = icon(
  "Target",
  <>
    <circle cx="12" cy="12" r="10" />
    <circle cx="12" cy="12" r="6" />
    <circle cx="12" cy="12" r="2" />
  </>,
);
export const GraduationCap = icon("GraduationCap", <path d="M22 10v6M2 10l10-5 10 5-10 5zM6 12v5c3 3 9 3 12 0v-5" />);
export const Handshake = icon(
  "Handshake",
  <path d="m11 17 2 2a1 1 0 1 0 3-3m-2-2 2.5 2.5a1 1 0 1 0 3-3l-3.88-3.88a3 3 0 0 0-4.24 0l-.88.88a1 1 0 1 1-3-3l2.81-2.81a5.79 5.79 0 0 1 7.06-.87l.47.28a2 2 0 0 0 1.42.25L21 4m0-1 1 11h-2M3 3 2 14l6.5 6.5a1 1 0 1 0 3-3M3 4h8" />,
);
export const Bell = icon(
  "Bell",
  <path d="M3 20a1 1 0 0 1-1-1v-1a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v1a1 1 0 0 1-1 1zM20 16a8 8 0 1 0-16 0M12 4v4M10 4h4" />,
);
export const Wallet = icon(
  "Wallet",
  <path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4" />,
);
export const ArrowDownLeft = icon("ArrowDownLeft", <path d="M17 7 7 17M17 17H7V7" />);

/** A sphere's mark — the config's emoji, which Telegram shows as it is — drawn here as a line icon of this set; an
 * emoji it has no icon for is a neutral one, so a screen never shows emoji as its icons. */
const MARKS: Record<string, (props: IconProps) => React.JSX.Element> = {
  "💼": Briefcase,
  "✈": Plane,
  "🧳": Luggage,
  "💳": CreditCard,
  "💰": Wallet,
  "🩺": Stethoscope,
  "📣": Megaphone,
  "🎯": Target,
  "📚": Book,
  "📖": Book,
  "🎓": GraduationCap,
  "🤝": Handshake,
  "🛎": Bell,
  "🏠": Home,
};

export function SphereMark({ icon: mark, ...props }: IconProps & { icon: string | null | undefined }) {
  if (!mark) return null;
  const Mark = MARKS[mark.replace(/️/g, "").trim()] ?? Layers;
  return <Mark {...props} />;
}
