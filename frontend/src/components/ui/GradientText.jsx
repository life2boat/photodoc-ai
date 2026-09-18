import React from 'react';

export default function GradientText({ children, className = '' }) {
  return (
    <span
      className={`bg-clip-text text-transparent bg-[linear-gradient(to_right,#eab308,#fef08a,#eab308)] bg-[length:200%_auto] animate-gradient ${className}`}
    >
      {children}
    </span>
  );
}
