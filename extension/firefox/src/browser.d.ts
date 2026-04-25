/** Minimal Firefox WebExtension API type declarations. */
declare namespace browser {
  namespace alarms {
    interface Alarm {
      name: string;
      scheduledTime: number;
      periodInMinutes?: number;
    }
    interface AlarmInfo {
      when?: number;
      delayInMinutes?: number;
      periodInMinutes?: number;
    }
    function create(name: string, alarmInfo: AlarmInfo): void;
    function clear(name: string): Promise<boolean>;
    const onAlarm: {
      addListener(callback: (alarm: Alarm) => void): void;
    };
  }
  namespace runtime {
    const id: string;
    interface MessageSender {
      id?: string;
      tab?: { id: number; url?: string };
    }
    interface Manifest {
      version: string;
      name?: string;
      description?: string;
    }
    function getManifest(): Manifest;
    function sendMessage(message: unknown): Promise<unknown>;
    function sendNativeMessage(host: string, message: unknown): Promise<unknown>;
    const onMessage: {
      addListener(
        callback: (
          message: unknown,
          sender: MessageSender,
        ) => void | Promise<unknown>,
      ): void;
    };
  }
  namespace contentScripts {
    interface RegisteredContentScript {
      unregister(): Promise<void>;
    }
    interface ContentScriptOptions {
      matches: string[];
      js?: Array<{ file: string }>;
      css?: Array<{ file: string }>;
      runAt?: "document_start" | "document_end" | "document_idle";
    }
    function register(
      options: ContentScriptOptions,
    ): Promise<RegisteredContentScript>;
  }
  namespace storage {
    interface StorageChange {
      oldValue?: unknown;
      newValue?: unknown;
    }
    const local: {
      get(keys: string[]): Promise<Record<string, unknown>>;
      set(items: Record<string, unknown>): Promise<void>;
    };
    const onChanged: {
      addListener(
        callback: (
          changes: Record<string, StorageChange>,
          area: string,
        ) => void,
      ): void;
    };
  }
}
