import React from 'react';
import { motion as Motion } from 'framer-motion';

export default function FadeInSection({ children, className = '' }) {
  return (
    <Motion.div
      initial={{ opacity: 0, y: 40 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-100px" }}
      transition={{ duration: 0.6, ease: "easeOut" }}
      className={className}
    >
      {children}
    </Motion.div>
  );
}
