import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function IconFrame({ children, ...props }: IconProps): React.JSX.Element {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      focusable="false"
      viewBox="0 0 24 24"
      {...props}
    >
      {children}
    </svg>
  );
}

export function AgentsIcon(props: IconProps): React.JSX.Element {
  return (
    <IconFrame {...props}>
      <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="17" cy="10" r="2.5" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M2.8 19c.7-3.2 2.4-4.8 5.2-4.8s4.5 1.6 5.2 4.8M13.7 15.7c.9-1 2-1.5 3.5-1.5 2.2 0 3.6 1.3 4 3.9"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.8"
      />
    </IconFrame>
  );
}

export function MinusIcon(props: IconProps): React.JSX.Element {
  return (
    <IconFrame {...props}>
      <path
        d="M5 12h14"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="2"
      />
    </IconFrame>
  );
}

export function PlusIcon(props: IconProps): React.JSX.Element {
  return (
    <IconFrame {...props}>
      <path
        d="M12 5v14M5 12h14"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="2"
      />
    </IconFrame>
  );
}

export function FitIcon(props: IconProps): React.JSX.Element {
  return (
    <IconFrame {...props}>
      <path
        d="M9 5H5v4m10-4h4v4M9 19H5v-4m10 4h4v-4"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </IconFrame>
  );
}

export function ReadIcon(props: IconProps): React.JSX.Element {
  return (
    <IconFrame {...props}>
      <path
        d="M3.5 12s3.1-5 8.5-5 8.5 5 8.5 5-3.1 5-8.5 5-8.5-5-8.5-5Z"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <circle cx="12" cy="12" r="2.2" stroke="currentColor" strokeWidth="1.7" />
    </IconFrame>
  );
}

export function WriteIcon(props: IconProps): React.JSX.Element {
  return (
    <IconFrame {...props}>
      <path
        d="m5 16-.8 3.8L8 19l9.7-9.7-3-3L5 16Zm8.4-8.4 3 3"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </IconFrame>
  );
}
