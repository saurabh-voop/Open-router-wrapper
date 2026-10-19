"use client";

import { useState } from "react";
import { AppFrame } from "../AppFrame";
import { Icon, IconButton } from "../ui";
import { ChatApp } from "./ChatApp";

export function ChatPage() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  return (
    <AppFrame
      headerStart={
        <IconButton
          label={sidebarOpen ? "Hide conversations" : "Show conversations"}
          className="md:hidden"
          aria-expanded={sidebarOpen}
          onClick={() => setSidebarOpen((v) => !v)}
        >
          <Icon.Menu />
        </IconButton>
      }
    >
      <ChatApp sidebarOpen={sidebarOpen} onSidebarOpenChange={setSidebarOpen} />
    </AppFrame>
  );
}
