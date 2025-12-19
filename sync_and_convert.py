#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Automated sync script for Noteshelf .nsa files from cloud storage.
Supports: Google Drive, Dropbox, OneDrive, WebDAV
"""

import os
import sys
import time
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# Import conversion function from our converter
from nsa_convertor import nsa_to_pdf

from dotenv import load_dotenv

load_dotenv()

DEFAULT_PATH = os.getenv("DEFAULT_PATH", r"./output")
class CloudSyncManager:
    """Base class for cloud storage sync managers"""
    
    def __init__(self, local_dir: str, output_dir: str, state_file: str = "sync_state.json"):
        self.local_dir = Path(local_dir)
        self.output_dir = Path(output_dir)
        self.state_file = Path(state_file)
        self.state = self._load_state()
        
        # Create directories if they don't exist
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_state(self) -> Dict:
        """Load sync state from JSON file"""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {"files": {}, "last_sync": None}
    
    def _save_state(self):
        """Save sync state to JSON file"""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def _file_hash(self, filepath: Path) -> str:
        """Calculate MD5 hash of file"""
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    def _needs_conversion(self, nsa_file: Path, pdf_path: Optional[Path] = None) -> bool:
        """Check if NSA file needs conversion"""
        file_key = str(nsa_file)
        if pdf_path is None:
            pdf_file = self.output_dir / (nsa_file.stem + ".pdf")
        else:
            pdf_file = pdf_path
        
        # Convert if PDF doesn't exist
        if not pdf_file.exists():
            return True
        
        # Convert if file hash changed
        current_hash = self._file_hash(nsa_file)
        stored_hash = self.state["files"].get(file_key, {}).get("hash")
        
        return current_hash != stored_hash
    
    def convert_file(self, nsa_file: Path, pdf_path: Optional[Path] = None, verbose: bool = True, metadata: dict = None) -> bool:
        """Convert single NSA file to PDF"""
        try:
            if metadata is None:
                metadata = {}
                
            if pdf_path is None:
                pdf_file = self.output_dir / (nsa_file.stem + ".pdf")
            else:
                pdf_file = pdf_path
            
            # Create parent directory if it doesn't exist
            pdf_file.parent.mkdir(parents=True, exist_ok=True)
            
            if verbose:
                print(f"Converting: {nsa_file.name}")
            
            nsa_to_pdf(
                str(nsa_file), 
                str(pdf_file), 
                verbose=False,  # Don't show conversion details to avoid clutter
                desired_highlighter_ratio=5.0,
                highlighter_opacity=0.35,
                smooth=True,
                epsilon=0.8
            )
            
            if verbose:
                # Show relative path from output_dir
                try:
                    rel_path = pdf_file.relative_to(self.output_dir)
                    print(f"  ✓ Saved to: {rel_path}")
                except ValueError:
                    print(f"  ✓ Saved to: {pdf_file.name}")
            
            # Update state
            file_key = str(nsa_file)
            self.state["files"][file_key] = {
                "hash": self._file_hash(nsa_file),
                "last_converted": datetime.now().isoformat(),
                "pdf_path": str(pdf_file),
                "gdrive_md5": metadata.get('md5_checksum'),  # Store GDrive checksum
                "gdrive_modified": metadata.get('modified_time')
            }
            self._save_state()
            
            return True
        except Exception as e:
            print(f"❌ Error converting {nsa_file.name}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def sync_local_files(self, verbose: bool = True) -> int:
        """Process all .nsa files in local directory"""
        converted = 0
        nsa_files = list(self.local_dir.glob("*.nsa"))
        
        if verbose:
            print(f"Found {len(nsa_files)} .nsa files in {self.local_dir}")
        
        for nsa_file in nsa_files:
            if self._needs_conversion(nsa_file):
                if self.convert_file(nsa_file, verbose):
                    converted += 1
        
        self.state["last_sync"] = datetime.now().isoformat()
        self._save_state()
        
        return converted


class GoogleDriveSync(CloudSyncManager):
    """Google Drive sync implementation"""
    
    def __init__(self, local_dir: str, output_dir: str, folder_id: Optional[str] = None, recursive: bool = True):
        super().__init__(local_dir, output_dir)
        self.folder_id = folder_id
        self.recursive = recursive
        self.service = None
        self.file_metadata = {}  # Track folder paths for each file
    
    def authenticate(self):
        """Authenticate with Google Drive API"""
        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            import pickle
            
            SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
            creds = None
            
            # Token file stores user's access and refresh tokens
            if os.path.exists('token.pickle'):
                with open('token.pickle', 'rb') as token:
                    creds = pickle.load(token)
            
            # If no valid credentials, let user log in
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                
                # Save credentials for next run
                with open('token.pickle', 'wb') as token:
                    pickle.dump(creds, token)
            
            self.service = build('drive', 'v3', credentials=creds)
            return True
        except ImportError:
            print("Error: Google Drive API not installed.")
            print("Install with: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client")
            return False
        except Exception as e:
            print(f"Authentication error: {e}")
            return False
    
    def _get_all_folders_recursive(self, parent_id: str, parent_path: str = "") -> Dict[str, str]:
        """Get all folder IDs recursively under a parent folder with their paths
        Returns: Dict mapping folder_id to folder_path
        """
        folder_map = {parent_id: parent_path}
        
        # Get subfolders
        query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder'"
        results = self.service.files().list(
            q=query,
            fields="files(id, name)"
        ).execute()
        
        subfolders = results.get('files', [])
        
        # Recursively get subfolders
        for folder in subfolders:
            folder_path = os.path.join(parent_path, folder['name']) if parent_path else folder['name']
            subfolder_map = self._get_all_folders_recursive(folder['id'], folder_path)
            folder_map.update(subfolder_map)
        
        return folder_map
    
    def download_files(self, verbose: bool = True) -> int:
        """Download .nsa files from Google Drive"""
        if not self.service:
            if not self.authenticate():
                return 0
        
        try:
            from googleapiclient.http import MediaIoBaseDownload
            import io
            
            # Get all folder IDs to search (recursive or single)
            folder_map = {}  # Maps folder_id to folder_path
            if self.folder_id:
                if self.recursive:
                    if verbose:
                        print(f"Searching folder and all subfolders...")
                    folder_map = self._get_all_folders_recursive(self.folder_id)
                    if verbose:
                        print(f"Found {len(folder_map)} folders to search")
                else:
                    folder_map = {self.folder_id: ""}
            
            # Query for .nsa files
            if folder_map:
                # Build query for multiple folders
                folder_ids = list(folder_map.keys())
                folder_queries = [f"'{fid}' in parents" for fid in folder_ids]
                query = f"({' or '.join(folder_queries)}) and name contains '.nsa' and mimeType != 'application/vnd.google-apps.folder'"
            else:
                query = "name contains '.nsa' and mimeType != 'application/vnd.google-apps.folder'"
            
            results = self.service.files().list(
                q=query,
                fields="files(id, name, modifiedTime, md5Checksum, parents)",
                pageSize=1000
            ).execute()
            
            files = results.get('files', [])
            downloaded = 0
            skipped = 0
            
            if verbose:
                print(f"Found {len(files)} .nsa files on Google Drive")
            
            for file in files:
                local_path = self.local_dir / file['name']
                
                # Determine folder path for this file
                folder_path = ""
                if 'parents' in file and file['parents']:
                    parent_id = file['parents'][0]  # Use first parent
                    folder_path = folder_map.get(parent_id, "")
                
                # Store metadata for later conversion
                self.file_metadata[file['name']] = {
                    'folder_path': folder_path,
                    'file_id': file['id'],
                    'md5_checksum': file.get('md5Checksum'),
                    'modified_time': file.get('modifiedTime')
                }
                
                # Check if we need to download this file
                state_key = str(local_path)
                stored_state = self.state["files"].get(state_key, {})
                stored_checksum = stored_state.get("gdrive_md5")
                current_checksum = file.get('md5Checksum')
                
                # Skip download if file exists locally and hasn't changed on GDrive
                if (local_path.exists() and 
                    current_checksum and 
                    stored_checksum == current_checksum):
                    if verbose:
                        print(f"Skipping {file['name']} (unchanged)")
                    skipped += 1
                    
                    # Update state to ensure metadata is current
                    self.state["files"][state_key] = {
                        **stored_state,
                        "gdrive_md5": current_checksum,
                        "gdrive_modified": file.get('modifiedTime')
                    }
                    continue
                
                # Download file
                if verbose:
                    print(f"Downloading {file['name']}...")
                    
                request = self.service.files().get_media(fileId=file['id'])
                fh = io.FileIO(local_path, 'wb')
                downloader = MediaIoBaseDownload(fh, request)
                
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if verbose and status:
                        print(f"  Progress: {int(status.progress() * 100)}%")
                
                # Update state after download with GDrive metadata
                local_hash = self._file_hash(local_path)
                self.state["files"][state_key] = {
                    **stored_state,
                    "hash": local_hash,
                    "gdrive_md5": current_checksum,
                    "gdrive_modified": file.get('modifiedTime'),
                    "last_downloaded": datetime.now().isoformat()
                }
                
                downloaded += 1
            
            # Save state after all downloads
            self._save_state()
            
            if verbose:
                print(f"\nSummary: Downloaded {downloaded}, Skipped {skipped} (unchanged)")
            
            return downloaded
        except Exception as e:
            print(f"Error downloading files: {e}")
            return 0
    
    def sync(self, verbose: bool = True) -> tuple[int, int]:
        """Download and convert files"""
        downloaded = self.download_files(verbose)
        
        # Convert files with folder structure
        converted = 0
        nsa_files = list(self.local_dir.glob("*.nsa"))
        
        if verbose:
            print(f"\nProcessing {len(nsa_files)} .nsa files for conversion")
        
        for nsa_file in nsa_files:
            # Get folder path from metadata
            metadata = self.file_metadata.get(nsa_file.name, {})
            folder_path = metadata.get('folder_path', '')
            
            # Construct PDF output path with folder structure
            if folder_path:
                pdf_path = self.output_dir / folder_path / (nsa_file.stem + ".pdf")
            else:
                pdf_path = self.output_dir / (nsa_file.stem + ".pdf")
            
            # Check if conversion is needed
            if self._needs_conversion(nsa_file, pdf_path):
                if self.convert_file(nsa_file, pdf_path, verbose, metadata):
                    converted += 1
        
        self.state["last_sync"] = datetime.now().isoformat()
        self._save_state()
        
        return downloaded, converted


def main():
    parser = argparse.ArgumentParser(
        description="Sync Noteshelf .nsa files from cloud storage and convert to PDF"
    )
    parser.add_argument(
        "--provider",
        choices=["local", "gdrive", "dropbox", "onedrive", "webdav"],
        default="local",
        help="Cloud storage provider"
    )
    parser.add_argument(
        "--local-dir",
        default="./nsa_files",
        help="Local directory for .nsa files"
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_PATH,
        help="Output directory for PDFs"
    )
    parser.add_argument(
        "--folder-id",
        help="Google Drive folder ID (optional)"
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Don't search subfolders (only search specified folder)"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode - continuously sync at interval"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Sync interval in seconds (default: 300 = 5 minutes)"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output"
    )
    
    args = parser.parse_args()
    verbose = not args.quiet
    
    # Initialize sync manager based on provider
    if args.provider == "gdrive":
        recursive = not args.no_recursive
        manager = GoogleDriveSync(args.local_dir, args.output_dir, args.folder_id, recursive)
    else:
        manager = CloudSyncManager(args.local_dir, args.output_dir)
    
    def sync_once():
        """Perform one sync operation"""
        if verbose:
            print(f"\n{'='*60}")
            print(f"Sync started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")
        
        if args.provider == "gdrive":
            downloaded, converted = manager.sync(verbose)
            if verbose:
                print(f"\nDownloaded: {downloaded} files")
                print(f"Converted: {converted} files")
        else:
            # Local mode - just convert existing files
            converted = manager.sync_local_files(verbose)
            if verbose:
                print(f"\nConverted: {converted} files")
        
        if verbose:
            print(f"Output directory: {manager.output_dir}")
    
    # Run sync
    if args.watch:
        if verbose:
            print(f"Watch mode enabled - syncing every {args.interval} seconds")
            print("Press Ctrl+C to stop")
        
        try:
            while True:
                sync_once()
                if verbose:
                    print(f"\nWaiting {args.interval} seconds until next sync...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            if verbose:
                print("\nSync stopped by user")
    else:
        sync_once()


if __name__ == "__main__":
    main()
