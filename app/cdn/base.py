from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple

class CDNProvider(ABC):
    """Abstract Base Class for CDN Storage Providers."""
    
    def __init__(self, api_key: str, config: Dict[str, Any] = None):
        self.api_key = api_key
        self.config = config or {}

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str]:
        """Test API Key connection with provider."""
        pass

    @abstractmethod
    def upload_file(self, local_file_path: str, remote_filename: str = None) -> Dict[str, Any]:
        """
        Upload a local file to the CDN.
        Returns dict with:
          - 'url': Direct public HTTP link
          - 'remote_path': Remote identifier or path
          - 'file_size': Size in bytes
        """
        pass

    @abstractmethod
    def delete_file(self, remote_path_or_url: str) -> bool:
        """Delete a file from the CDN by identifier or URL."""
        pass

    @abstractmethod
    def get_storage_info(self) -> Dict[str, int]:
        """
        Get storage info dict:
          - 'used_bytes'
          - 'available_bytes'
          - 'total_bytes'
        """
        pass
