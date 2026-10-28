// A Record's or a Showcase's markdown: the transcript's rules (no raw HTML, nothing that loads by itself, images as
// text), and a link the platform turned into an app route (`/spaces/…`, no `.`/`..`/empty segment, no query or
// fragment: `isAppHref`) opens here, in the app. Any other link keeps Streamdown's own, with its confirmation.
import { useRouter } from "@tanstack/react-router";
import type { ComponentProps } from "react";
import { defaultComponents, type ExtraProps, Streamdown } from "streamdown";
import { BLOCKED, ImageAsText, REHYPE } from "@/components/transcript/item";
import { isAppHref } from "@/lib/spaces";
import { cn } from "@/lib/utils";

const OutLink = defaultComponents.a;

function AppLink({ href, children, node, ...rest }: ComponentProps<"a"> & ExtraProps) {
  const router = useRouter();
  if (isAppHref(href)) {
    return (
      <a
        href={href}
        className="font-medium text-primary underline underline-offset-2"
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          router.history.push(href);
        }}
      >
        {children}
      </a>
    );
  }
  return (
    <OutLink href={href} node={node} {...rest}>
      {children}
    </OutLink>
  );
}

const COMPONENTS = { img: ImageAsText, a: AppLink };

export function BaseMarkdown({ text, className }: { text: string; className?: string }) {
  return (
    <Streamdown
      className={cn("markdown text-[15px] leading-relaxed md:text-sm", className)}
      mode="static"
      controls={false}
      skipHtml
      rehypePlugins={REHYPE}
      disallowedElements={BLOCKED}
      unwrapDisallowed
      components={COMPONENTS}
      linkSafety={{ enabled: true }}
    >
      {text}
    </Streamdown>
  );
}
