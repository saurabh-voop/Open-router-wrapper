import { Suspense } from "react";
import { ChatPage } from "@/components/chat/ChatPage";
import { FullPageLoading } from "@/components/FullPageLoading";

export default function Home() {
  // The selected conversation lives in ?c=<id>; search params are request-time
  // data, so the client shell renders inside Suspense (cacheComponents).
  return (
    <Suspense fallback={<FullPageLoading />}>
      <ChatPage />
    </Suspense>
  );
}
