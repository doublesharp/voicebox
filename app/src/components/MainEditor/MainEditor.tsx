import { Sparkles, Upload } from 'lucide-react';
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { DoubleSharpMark } from '@/components/Branding';
import { FloatingGenerateBox } from '@/components/Generation/FloatingGenerateBox';
import { HistoryTable } from '@/components/History/HistoryTable';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useToast } from '@/components/ui/use-toast';
import { ProfileForm } from '@/components/VoiceProfiles/ProfileForm';

import { useImportProfile, useProfiles } from '@/lib/hooks/useProfiles';
import { useUIStore } from '@/stores/uiStore';

export function MainEditor() {
  const { t } = useTranslation();
  const { data: profiles, isLoading: profilesLoading, error: profilesError } = useProfiles();
  const setDialogOpen = useUIStore((state) => state.setProfileDialogOpen);
  const importProfile = useImportProfile();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [importDialogOpen, setImportDialogOpen] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const { toast } = useToast();

  const handleImportClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      if (!file.name.endsWith('.voicebox.zip')) {
        toast({
          title: t('main.import.invalidTitle'),
          description: t('main.import.invalidDescription'),
          variant: 'destructive',
        });
        return;
      }
      setSelectedFile(file);
      setImportDialogOpen(true);
    }
  };

  const handleImportConfirm = () => {
    if (selectedFile) {
      importProfile.mutate(selectedFile, {
        onSuccess: () => {
          setImportDialogOpen(false);
          setSelectedFile(null);
          if (fileInputRef.current) {
            fileInputRef.current.value = '';
          }
          toast({
            title: t('main.import.successTitle'),
            description: t('main.import.successDescription'),
          });
        },
        onError: (error) => {
          toast({
            title: t('main.import.failedTitle'),
            description: error.message,
            variant: 'destructive',
          });
        },
      });
    }
  };

  return (
    <div
      className="h-full min-h-0 overflow-y-auto"
      style={{ paddingBottom: 'calc(var(--app-player-height) + 1.5rem)' }}
    >
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Generate</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Create speech and listen to your recent takes.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" className="h-9 rounded-lg" onClick={handleImportClick}>
            <Upload className="mr-2 h-4 w-4" />
            {t('main.importVoice')}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".voicebox.zip"
            onChange={handleFileChange}
            className="hidden"
          />
          <Button variant="outline" className="h-9 rounded-lg" onClick={() => setDialogOpen(true)}>
            <Sparkles className="mr-2 h-4 w-4" />
            {t('main.createVoice')}
          </Button>
        </div>
      </header>
      <div className="grid grid-cols-1 gap-6 min-[1100px]:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <section className="min-w-0" aria-label="Generate speech">
          {profilesError && (
            <p role="alert" className="mb-4 text-sm text-destructive">
              {t('profiles.list.errorLoading', { message: profilesError.message })}
            </p>
          )}
          {!profilesLoading && !profilesError && profiles?.length === 0 && (
            <div className="mb-4 flex items-center gap-3 rounded-[10px] border border-border/60 bg-card/40 p-4">
              <DoubleSharpMark className="h-10 w-10 opacity-60" />
              <div>
                <p className="text-sm font-medium">Create your first voice</p>
                <p className="text-xs text-muted-foreground">
                  Create or import a voice to start generating speech.
                </p>
              </div>
              <Button
                variant="outline"
                className="ml-auto h-9 rounded-lg"
                onClick={() => setDialogOpen(true)}
              >
                Create voice
              </Button>
            </div>
          )}
          <FloatingGenerateBox layout="docked" />
        </section>
        <section className="min-w-0" aria-labelledby="recent-takes-title">
          <h2 id="recent-takes-title" className="mb-3 text-[15px] font-semibold">
            Recent takes
          </h2>
          <HistoryTable embedded />
        </section>
      </div>
      <ProfileForm />

      <Dialog open={importDialogOpen} onOpenChange={setImportDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('main.import.dialogTitle')}</DialogTitle>
            <DialogDescription>
              {t('main.import.dialogDescription', { name: selectedFile?.name })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setImportDialogOpen(false);
                setSelectedFile(null);
                if (fileInputRef.current) {
                  fileInputRef.current.value = '';
                }
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              onClick={handleImportConfirm}
              disabled={importProfile.isPending || !selectedFile}
            >
              {importProfile.isPending ? t('main.import.importing') : t('main.import.action')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
