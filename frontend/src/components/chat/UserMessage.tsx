import type { Message } from "@/lib/api";
import { AttachmentChip } from "./parts";

export function UserMessage({ msg }: { msg: Message }) {
  return (
    <div className="flex justify-end" data-testid="user-message">
      <div className="max-w-[85%] space-y-1.5">
        {msg.attachments.length > 0 && (
          <div className="flex flex-wrap justify-end gap-1.5">
            {msg.attachments.map((a) => (
              <AttachmentChip key={a.id} attachment={a} />
            ))}
          </div>
        )}
        {msg.text && (
          <div className="rounded-2xl rounded-br-md bg-surface-2 px-3.5 py-2 text-sm whitespace-pre-wrap break-words">
            {msg.text}
          </div>
        )}
      </div>
    </div>
  );
}
